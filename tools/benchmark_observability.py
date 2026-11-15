"""Run a repeatable, isolated synthetic benchmark of VAOS observability storage.

This tool creates a temporary SQLite database and never reads the configured
VAOS data directory. Results are local function timings, not network or target-
hardware performance claims.

Example from the repository root::

    .venv/Scripts/python.exe tools/benchmark_observability.py --events 2000 --queries 20
"""
from __future__ import annotations

import argparse
import json
import math
import platform
import sqlite3
import statistics
import sys
import tempfile
import time
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

import observability_store as store  # noqa: E402


def _measure_queries(operation: Callable[[], object], repetitions: int) -> dict[str, float | int]:
    operation()  # Warm the interpreter and SQLite page cache before sampling.
    samples_ms: list[float] = []
    for _ in range(repetitions):
        started = time.perf_counter()
        operation()
        samples_ms.append((time.perf_counter() - started) * 1000)
    ordered = sorted(samples_ms)
    p95_index = min(len(ordered) - 1, math.ceil(0.95 * len(ordered)) - 1)
    return {
        "runs": repetitions,
        "p50_ms": round(statistics.median(samples_ms), 3),
        "p95_ms": round(ordered[p95_index], 3),
        "max_ms": round(max(samples_ms), 3),
    }


def run(events: int, queries: int) -> dict[str, object]:
    with tempfile.TemporaryDirectory(prefix="vaos-observability-benchmark-") as temp_dir:
        database = Path(temp_dir) / "observability.sqlite3"
        store.initialize_database(database)
        timestamp_start = datetime.now(timezone.utc) - timedelta(minutes=5)

        started = time.perf_counter()
        for index in range(events):
            timestamp = (timestamp_start + timedelta(milliseconds=index)).isoformat(timespec="microseconds").replace("+00:00", "Z")
            store.record_event(
                database,
                event_id=f"benchmark-event-{index:08d}",
                timestamp=timestamp,
                event_type="api.request",
                component="benchmark",
                message=f"synthetic benchmark request {index}",
                counter_deltas={"api.requests": 1},
                counter_dimensions={"api.requests": {"route": "/api/v1/benchmark", "method": "GET", "status": 200}},
            )
        event_ingest_seconds = time.perf_counter() - started

        buffer_time = datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")
        started = time.perf_counter()
        for _ in range(events):
            store.buffer_counter_delta(
                database,
                metric_key="api.requests",
                delta=1,
                dimensions={"route": "/api/v1/buffer-benchmark", "method": "GET", "status": 200},
                timestamp=buffer_time,
            )
        counter_buffer_seconds = time.perf_counter() - started
        with closing(sqlite3.connect(database)) as connection:
            buffer_rows = int(connection.execute("SELECT COUNT(*) FROM observability_counter_buffer").fetchone()[0])
            buffered_increment_total = int(connection.execute("SELECT COALESCE(SUM(delta),0) FROM observability_counter_buffer").fetchone()[0])
        flush_started = time.perf_counter()
        flushed_increments = store.flush_counter_buffer(database, maximum=events)
        counter_flush_seconds = time.perf_counter() - flush_started

        after = (timestamp_start - timedelta(seconds=1)).isoformat(timespec="seconds").replace("+00:00", "Z")
        before = (datetime.now(timezone.utc) + timedelta(seconds=1)).isoformat(timespec="seconds").replace("+00:00", "Z")
        operations: dict[str, Callable[[], object]] = {
            "event_text_search": lambda: store.query_events(database, search="synthetic benchmark request", limit=100),
            "filtered_event_page": lambda: store.query_events(database, event_type="api.request", component="benchmark", limit=100),
            "metric_series": lambda: store.query_metric_series(
                database,
                metric_key="api.requests",
                after=after,
                before=before,
                limit=500,
                dimensions={"route": "/api/v1/benchmark"},
            ),
            "lifetime_overview": lambda: store.overview(database, period="lifetime"),
        }
        query_results = {name: _measure_queries(operation, queries) for name, operation in operations.items()}
        file_bytes = database.stat().st_size

    return {
        "synthetic_events": events,
        "query_repetitions_each": queries,
        "event_write": {
            "total_seconds": round(event_ingest_seconds, 3),
            "events_per_second": round(events / event_ingest_seconds, 1),
        },
        "buffered_counter_write": {
            "total_seconds": round(counter_buffer_seconds, 3),
            "increments_per_second": round(events / counter_buffer_seconds, 1),
            "buffer_rows_before_flush": buffer_rows,
            "buffered_increment_total": buffered_increment_total,
            "flush_seconds": round(counter_flush_seconds, 3),
            "increments_persisted": flushed_increments,
        },
        "queries": query_results,
        "database_bytes": file_bytes,
        "environment": {"python": platform.python_version(), "platform": platform.platform()},
        "scope": "temporary SQLite; same-process function timings; query cache warmed; not a network, concurrent-load, or target-hardware benchmark",
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--events", type=int, default=2000, help="synthetic events and buffered increments (1–20000)")
    parser.add_argument("--queries", type=int, default=20, help="timed query repetitions per operation (1–2000)")
    args = parser.parse_args()
    if not 1 <= args.events <= 20_000:
        parser.error("--events must be between 1 and 20000")
    if not 1 <= args.queries <= 2_000:
        parser.error("--queries must be between 1 and 2000")
    print(json.dumps(run(args.events, args.queries), indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
