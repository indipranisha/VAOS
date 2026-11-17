"""No-network parser, date conversion and credential policy tests for vaos CLI."""
from __future__ import annotations

import unittest
import tempfile
import json
import shlex
from datetime import datetime, timedelta, timezone
from io import StringIO
from unittest.mock import patch
from pathlib import Path

import vaos_cli


class VaosCliTests(unittest.TestCase):
    def test_date_only_range_uses_selected_timezone_and_end_is_exclusive(self):
        start = vaos_cli._iso_boundary("2026-11-12", timezone_name="Asia/Kolkata")
        end = vaos_cli._iso_boundary("2026-11-12", end=True, timezone_name="Asia/Kolkata")
        self.assertEqual(start, "2026-11-11T18:30:00Z")
        self.assertEqual(end, "2026-11-12T18:30:00Z")

    def test_naive_datetime_uses_selected_timezone_and_invalid_zone_fails(self):
        parsed = vaos_cli._iso_boundary("2026-11-12T12:00:00", timezone_name="Asia/Kolkata")
        self.assertEqual(parsed, "2026-11-12T06:30:00Z")
        with self.assertRaisesRegex(vaos_cli.VaosCliError, "Unknown timezone"):
            vaos_cli._iso_boundary("2026-11-12", timezone_name="Not/A_Zone")

    def test_cli_commands_cover_requested_terminal_workflows(self):
        parser = vaos_cli.build_parser()
        self.assertEqual(parser.parse_args(["status"]).command, "status")
        overview = parser.parse_args(["stats", "--period", "7d"])
        self.assertEqual((overview.stats_action, overview.period), ("overview", "7d"))
        metrics = parser.parse_args(["stats", "metrics", "--group-by", "day", "--from", "2026-11-01", "--to", "2026-11-12"])
        self.assertEqual(metrics.stats_action, "metrics")
        logs = parser.parse_args(["logs", "search", "--component", "training", "--level", "ERROR", "--session-id", "session-abc", "--correlation-id", "run-abc", "--training-job-id", "job-42", "--dataset-id", "idd-lite", "--model-id", "best.pt"])
        self.assertEqual((logs.logs_action, logs.component, logs.level, logs.session_id, logs.correlation_id, logs.training_job_id, logs.dataset_id, logs.model_id), ("search", "training", "ERROR", "session-abc", "run-abc", "job-42", "idd-lite", "best.pt"))
        follow = parser.parse_args(["logs", "tail", "--follow"])
        one_poll = parser.parse_args(["logs", "tail", "--no-follow"])
        self.assertTrue(follow.follow)
        self.assertFalse(one_poll.follow)
        report = parser.parse_args(["reports", "export", "security-events", "--format", "csv", "--output", "security.csv", "--from", "2026-11-01", "--to", "2026-11-12"])
        self.assertEqual(report.report_name, "security-events")
        with patch("sys.stderr", StringIO()), self.assertRaises(SystemExit):
            parser.parse_args(["reports", "export", "model-evaluations", "--output", "arena.csv"])
        self.assertEqual(report.report_format, "csv")
        event_report = parser.parse_args(["reports", "export", "logs", "--format", "csv", "--output", "events.csv", "--from", "2026-11-01", "--to", "2026-11-12", "--session-id", "session-abc", "--correlation-id", "run-abc", "--training-job-id", "job-42", "--dataset-id", "idd-lite", "--model-id", "best.pt"])
        self.assertEqual((event_report.session_id, event_report.correlation_id, event_report.training_job_id, event_report.dataset_id, event_report.model_id), ("session-abc", "run-abc", "job-42", "idd-lite", "best.pt"))
        unrelated_report = parser.parse_args(["reports", "export", "security-events", "--format", "csv", "--output", "audit.csv", "--from", "2026-11-01", "--to", "2026-11-12", "--correlation-id", "run-abc"])
        with self.assertRaisesRegex(vaos_cli.VaosCliError, "only to a logs/events report"):
            vaos_cli._command_reports(object(), unrelated_report)
        invalid_actor_report = parser.parse_args(["reports", "export", "logs", "--output", "events.csv", "--from", "2026-11-01", "--to", "2026-11-12", "--actor-ref", "local-admin"])
        with self.assertRaisesRegex(vaos_cli.VaosCliError, "only to a security-events report"):
            vaos_cli._command_reports(object(), invalid_actor_report)
        long_actor_report = parser.parse_args(["reports", "export", "security-events", "--output", "audit.csv", "--from", "2026-11-01", "--to", "2026-11-12", "--actor-ref", "x" * 121])
        with self.assertRaisesRegex(vaos_cli.VaosCliError, "120-character limit") as error:
            vaos_cli._command_reports(object(), long_actor_report)
        self.assertEqual(error.exception.exit_code, vaos_cli.EXIT_USAGE)
        self.assertTrue(hasattr(parser.parse_args(["integrations", "status"]), "timezone"))
        lower_level = parser.parse_args(["logs", "search", "--level", "error"])
        self.assertEqual(lower_level.level, "ERROR")

    def test_cli_reference_examples_match_the_real_argument_parser(self):
        reference = Path(__file__).resolve().parents[1] / "docs" / "observability" / "cli-reference.md"
        guide = reference.read_text(encoding="utf-8")
        examples = guide.split("### Examples", 1)[1].split("```sh", 1)[1].split("```", 1)[0]
        parser = vaos_cli.build_parser()
        commands = [shlex.split(line.strip()) for line in examples.splitlines() if line.strip()]
        self.assertGreater(len(commands), 0)
        for command in commands:
            with self.subTest(command=command):
                self.assertEqual(command[0], "vaos")
                parser.parse_args(command[1:])
        pipelines = guide.split("## Shell pipelines", 1)[1].split("```sh", 1)[1].split("```", 1)[0]
        for line in pipelines.splitlines():
            if line.strip():
                command = shlex.split(line.split("|", 1)[0].strip())
                with self.subTest(pipeline=line.strip()):
                    self.assertEqual(command[0], "vaos")
                    parser.parse_args(command[1:])

    def test_stats_default_action_requests_overview_endpoint(self):
        class FakeClient:
            def __init__(self): self.calls = []
            def request(self, method, path, *, query=None, payload=None, binary=False):
                self.calls.append((method, path, query))
                return {"period": "7d", "totals": {}}

        args = vaos_cli.build_parser().parse_args(["stats", "--period", "7d"])
        client = FakeClient()
        with patch("sys.stdout", StringIO()):
            vaos_cli._command_stats(client, args)
        self.assertEqual(client.calls, [("GET", "/admin/observability/overview", {"period": "7d"})])

    def test_log_search_cli_forwards_exact_session_correlation_and_training_identifiers(self):
        class FakeClient:
            def __init__(self): self.calls = []
            def request(self, method, path, *, query=None, payload=None, binary=False):
                self.calls.append((method, path, query))
                return {"items": [], "has_more": False}

        args = vaos_cli.build_parser().parse_args(["logs", "search", "--session-id", "session-abc", "--correlation-id", "run-abc", "--training-job-id", "job-42", "--dataset-id", "idd-lite", "--model-id", "best.pt"])
        client = FakeClient()
        self.assertEqual(vaos_cli._fetch_events(client, args, limit=50), [])
        self.assertEqual(client.calls[0][2]["correlation_id"], "run-abc")
        self.assertEqual(client.calls[0][2]["session_id"], "session-abc")
        self.assertEqual(client.calls[0][2]["training_job_id"], "job-42")
        self.assertEqual(client.calls[0][2]["dataset_id"], "idd-lite")
        self.assertEqual(client.calls[0][2]["model_id"], "best.pt")

    def test_log_list_cli_fetches_all_pages_up_to_requested_limit(self):
        class FakeClient:
            def __init__(self): self.calls = []
            def request(self, method, path, *, query=None, payload=None, binary=False):
                self.calls.append((method, path, dict(query or {})))
                if len(self.calls) == 1:
                    return {"items": [{"event_id": f"first-{i}"} for i in range(200)], "has_more": True, "next_cursor": "page-two"}
                return {"items": [{"event_id": f"second-{i}"} for i in range(50)], "has_more": False, "next_cursor": None}

        args = vaos_cli.build_parser().parse_args(["logs", "list", "--limit", "250", "--format", "json"])
        client = FakeClient(); output = StringIO()
        with patch("sys.stdout", output):
            vaos_cli._command_logs(client, args)
        rows = json.loads(output.getvalue())
        self.assertEqual(len(rows), 250)
        self.assertEqual(client.calls[0][2]["limit"], 200)
        self.assertEqual(client.calls[1][2]["cursor"], "page-two")
        self.assertEqual(client.calls[1][2]["limit"], 50)

    def test_log_list_cli_fetches_all_pages_up_to_requested_limit(self):
        class FakeClient:
            def __init__(self): self.calls = []
            def request(self, method, path, *, query=None, payload=None, binary=False):
                self.calls.append((method, path, dict(query or {})))
                if len(self.calls) == 1:
                    return {"items": [{"event_id": f"first-{i}"} for i in range(200)], "has_more": True, "next_cursor": "page-two"}
                return {"items": [{"event_id": f"second-{i}"} for i in range(50)], "has_more": False, "next_cursor": None}

        args = vaos_cli.build_parser().parse_args(["logs", "list", "--limit", "250", "--format", "json"])
        client = FakeClient(); output = StringIO()
        with patch("sys.stdout", output):
            vaos_cli._command_logs(client, args)
        rows = json.loads(output.getvalue())
        self.assertEqual(len(rows), 250)
        self.assertEqual(client.calls[0][2]["limit"], 200)
        self.assertEqual(client.calls[1][2]["cursor"], "page-two")
        self.assertEqual(client.calls[1][2]["limit"], 50)

    def test_event_report_cli_forwards_session_filter(self):
        class FakeClient:
            def __init__(self): self.calls = []
            def request(self, method, path, *, query=None, payload=None, binary=False):
                self.calls.append((method, path, query, payload, binary))
                if method == "POST": return {"job_id": "a" * 32, "status": "complete"}
                return b"csv-data", {"content-type": "text/csv"}

        with tempfile.TemporaryDirectory(prefix="vaos-cli-report-session-") as folder:
            output = str(Path(folder) / "events.csv")
            args = vaos_cli.build_parser().parse_args([
                "reports", "export", "logs", "--format", "csv", "--output", output,
                "--from", "2026-11-01", "--to", "2026-11-12", "--session-id", "session-abc", "--training-job-id", "job-42", "--dataset-id", "idd-lite", "--model-id", "best.pt",
            ])
            client = FakeClient()
            with patch("sys.stdout", StringIO()):
                vaos_cli._command_reports(client, args)
            self.assertEqual(client.calls[0][3]["filters"]["session_id"], "session-abc")
            self.assertEqual(client.calls[0][3]["filters"]["training_job_id"], "job-42")
            self.assertEqual(client.calls[0][3]["filters"]["dataset_id"], "idd-lite")
            self.assertEqual(client.calls[0][3]["filters"]["model_id"], "best.pt")
            self.assertEqual(Path(output).read_bytes(), b"csv-data")

    def test_security_report_cli_forwards_exact_actor_reference(self):
        class FakeClient:
            def __init__(self): self.calls = []
            def request(self, method, path, *, query=None, payload=None, binary=False):
                self.calls.append((method, path, query, payload, binary))
                if method == "POST": return {"job_id": "b" * 32, "status": "complete"}
                return b"csv-data", {"content-type": "text/csv"}

        with tempfile.TemporaryDirectory(prefix="vaos-cli-security-actor-") as folder:
            output = str(Path(folder) / "audit.csv")
            args = vaos_cli.build_parser().parse_args([
                "reports", "export", "security-events", "--format", "csv", "--output", output,
                "--from", "2026-11-01", "--to", "2026-11-12", "--actor-ref", "api-token:abc",
            ])
            client = FakeClient()
            with patch("sys.stdout", StringIO()):
                vaos_cli._command_reports(client, args)
            self.assertEqual(client.calls[0][3]["filters"]["actor_ref"], "api-token:abc")
            self.assertEqual(Path(output).read_bytes(), b"csv-data")

    def test_metrics_cli_includes_and_sums_counter_series(self):
        class FakeClient:
            def __init__(self):
                self.calls = []

            def request(self, method, path, *, query=None, payload=None, binary=False):
                self.calls.append((method, path, query))
                if path.endswith("/metrics"):
                    return {"items": [{"metric_key": "api.requests", "metric_type": "counter", "available": True, "unit": "requests"}]}
                return {"items": [
                    {"timestamp": "2026-11-11T10:10:00Z", "value": 2},
                    {"timestamp": "2026-11-11T10:40:00Z", "value": 3},
                ], "total": 2, "truncated": False}

        args = vaos_cli.build_parser().parse_args([
            "stats", "metrics", "--metric", "api.requests", "--group-by", "hour",
            "--from", "2026-11-11", "--to", "2026-11-12", "--dimension", "method=GET", "--format", "json",
        ])
        client = FakeClient()
        output = StringIO()
        with patch("sys.stdout", output):
            vaos_cli._command_stats(client, args)
        result = json.loads(output.getvalue())
        self.assertEqual(result[0]["total"], 5.0)
        self.assertEqual(result[0]["samples"], 2)
        series_query = next(query for _, path, query in client.calls if path.endswith("/series"))
        self.assertEqual(json.loads(series_query["dimensions"]), {"method": "GET"})

    def test_metrics_cli_rejects_truncated_series_instead_of_exporting_partial_totals(self):
        class FakeClient:
            def request(self, method, path, *, query=None, payload=None, binary=False):
                if path.endswith("/metrics"):
                    return {"items": [{"metric_key": "api.requests", "metric_type": "counter", "available": True, "unit": "requests"}]}
                return {"items": [{"timestamp": "2026-11-11T10:10:00Z", "value": 2}], "total": 1001, "truncated": True}

        args = vaos_cli.build_parser().parse_args([
            "stats", "metrics", "--metric", "api.requests", "--group-by", "day",
            "--from", "2026-11-11", "--to", "2026-11-12",
        ])
        with self.assertRaisesRegex(vaos_cli.VaosCliError, "Narrow the time range"):
            vaos_cli._command_stats(FakeClient(), args)

    def test_remote_plain_http_is_rejected_and_local_http_is_allowed(self):
        with patch.dict("os.environ", {"VAOS_API_TOKEN": "fixture-token"}, clear=False):
            with self.assertRaisesRegex(vaos_cli.VaosCliError, "require HTTPS"):
                vaos_cli.Client("http://example.com")
            client = vaos_cli.Client("http://127.0.0.1:8000")
            self.assertEqual(client.base, "http://127.0.0.1:8000")

    def test_output_timezone_converts_timestamps_and_renames_utc_fields(self):
        result = vaos_cli._localize_times({"started_at_utc": "2026-11-12T00:00:00Z"}, "Asia/Kolkata")
        self.assertEqual(result["started_at_local"], "2026-11-12T05:30:00+05:30")

    def test_text_log_output_writes_and_appends_to_requested_file(self):
        with tempfile.TemporaryDirectory(prefix="vaos-cli-") as folder:
            output = Path(folder) / "events.txt"
            vaos_cli._write_text_lines(["first event"], str(output))
            vaos_cli._write_text_lines(["second event"], str(output), append=True)
            self.assertEqual(output.read_text(encoding="utf-8"), "first event\nsecond event\n")

    def test_log_tail_catches_up_across_cursor_pages_without_dropping_or_repeating_events(self):
        origin = datetime(2026, 11, 13, tzinfo=timezone.utc)

        def event(event_id, seconds):
            stamp = (origin + timedelta(seconds=seconds)).isoformat(timespec="microseconds").replace("+00:00", "Z")
            return {"event_id": event_id, "timestamp": stamp, "level": "INFO", "component": "test", "event_type": "test.event", "message": event_id}

        initial = [event("old-2", 2), event("old-1", 1)]
        first_new_page = [event(f"new-{index:03}", index + 2) for index in range(250, 50, -1)]
        second_new_page = [event(f"new-{index:03}", index + 2) for index in range(50, 0, -1)] + initial

        class FakeClient:
            def __init__(self): self.calls = []
            def request(self, method, path, *, query=None, payload=None, binary=False):
                query = dict(query or {})
                self.calls.append((method, path, query))
                if len(self.calls) == 1:
                    return {"items": initial, "has_more": False, "next_cursor": None}
                if len(self.calls) == 2:
                    return {"items": first_new_page, "has_more": True, "next_cursor": "page-two"}
                if len(self.calls) == 3:
                    return {"items": second_new_page, "has_more": False, "next_cursor": None}
                return {"items": [], "has_more": False, "next_cursor": None}

        args = vaos_cli.build_parser().parse_args(["logs", "tail", "--limit", "2", "--format", "ndjson"])
        client = FakeClient()
        output = StringIO()
        with patch("sys.stdout", output), patch("vaos_cli.time.sleep", side_effect=[None, None, KeyboardInterrupt]):
            vaos_cli._command_logs(client, args)

        rows = [json.loads(line) for line in output.getvalue().splitlines()]
        event_ids = [row["event_id"] for row in rows]
        self.assertEqual(event_ids, ["old-1", "old-2", *[f"new-{index:03}" for index in range(1, 251)]])
        self.assertEqual(len(event_ids), len(set(event_ids)))
        self.assertEqual(client.calls[2][2]["cursor"], "page-two")
        self.assertEqual(client.calls[1][2]["after"], initial[-1]["timestamp"])

    def test_log_tail_inclusive_timestamp_watermark_captures_new_tied_event_once(self):
        stamp = "2026-11-13T00:00:02.000000Z"
        initial = [
            {"event_id": "middle", "timestamp": stamp, "level": "INFO", "component": "test", "event_type": "test.event", "message": "middle"},
            {"event_id": "older", "timestamp": "2026-11-13T00:00:01.000000Z", "level": "INFO", "component": "test", "event_type": "test.event", "message": "older"},
        ]
        tied_new = {"event_id": "a-lower-sort-key", "timestamp": stamp, "level": "INFO", "component": "test", "event_type": "test.event", "message": "late tied event"}

        class FakeClient:
            def __init__(self): self.calls = []
            def request(self, method, path, *, query=None, payload=None, binary=False):
                query = dict(query or {})
                self.calls.append(query)
                if len(self.calls) == 1:
                    return {"items": initial, "has_more": False, "next_cursor": None}
                if len(self.calls) == 2:
                    return {"items": [initial[0], tied_new], "has_more": False, "next_cursor": None}
                return {"items": [initial[0], tied_new], "has_more": False, "next_cursor": None}

        args = vaos_cli.build_parser().parse_args(["logs", "tail", "--limit", "2", "--format", "ndjson"])
        client = FakeClient()
        output = StringIO()
        with patch("sys.stdout", output), patch("vaos_cli.time.sleep", side_effect=[None, KeyboardInterrupt]):
            vaos_cli._command_logs(client, args)
        rows = [json.loads(line) for line in output.getvalue().splitlines()]
        self.assertEqual([row["event_id"] for row in rows], ["older", "middle", "a-lower-sort-key"])
        self.assertEqual(client.calls[1]["after"], initial[1]["timestamp"])

    def test_log_tail_requires_stream_safe_format_when_following(self):
        args = vaos_cli.build_parser().parse_args(["logs", "tail", "--format", "json"])
        with self.assertRaisesRegex(vaos_cli.VaosCliError, "text or NDJSON"):
            vaos_cli._command_logs(object(), args)

    def test_tail_seen_window_bounds_memory_without_splitting_tied_timestamp_group(self):
        same_stamp = "2026-11-13T00:00:00.000000Z"
        rows = [{"event_id": f"same-{index}", "timestamp": same_stamp} for index in range(5001)]
        with self.assertRaisesRegex(vaos_cli.VaosCliError, "share the same timestamp"):
            vaos_cli._bounded_tail_seen({}, rows)

        origin = datetime(2026, 11, 13, tzinfo=timezone.utc)
        distinct = [
            {"event_id": f"event-{index:04}", "timestamp": (origin + timedelta(seconds=index)).isoformat(timespec="microseconds").replace("+00:00", "Z")}
            for index in range(5001)
        ]
        retained = vaos_cli._bounded_tail_seen({}, distinct)
        self.assertEqual(len(retained), 5000)
        self.assertNotIn("event-0000", retained)
        self.assertIn("event-0001", retained)


if __name__ == "__main__":
    unittest.main()
