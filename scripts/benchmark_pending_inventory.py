"""Compare full and compact pending reads using synthetic queues, never live media."""

import argparse
import hashlib
import json
import platform
import statistics
import tempfile
import time
import tracemalloc
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Callable, Iterator
from unittest.mock import patch

from sqlalchemy import event

from mediaforce.core.db import DBClient, open_db, reset_engine_cache
from mediaforce.encoding import encode_queue
from scripts import benchmark_activity_refresh as baseline

CASES = (*baseline.CASES,
         baseline.WaitCase("never", True, profile="never"),
         baseline.WaitCase("retry_closed", False, status="retry_backoff", profile="fixture_night",
                           reason="Waiting for a host schedule window."),
         baseline.WaitCase("closed_storage", True, profile="fixture_night", reason="Waiting for shared storage."),
         baseline.WaitCase("closed_impossible", True, profile="fixture_night",
                           reason="Estimated runtime longer than every configured host schedule window"),
         baseline.WaitCase("closed_interrupted_storage", True, profile="fixture_night",
                           reason="Waiting for shared storage.", interrupted=True))
# Explicit case expectations, independent of the scheduler's classification implementation.
OPEN_WAITING = {"window_reason", "interrupted", "short_window", "never"}
NOTES = {"empty": "", "short": "Synthetic queue entry.", "wide": "Synthetic queue entry. " * 48}


def full_pending_read(connection: DBClient, *, limit: int) -> list[dict[str, Any]]:
    return encode_queue.list_encode_jobs(
        connection, statuses=encode_queue.QUEUED_ENCODE_JOB_STATUSES,
        job_kinds=encode_queue.RUNNABLE_ENCODE_JOB_KINDS, limit=limit,
    )


@contextmanager
def fixture_runtime(*, hour: int = 12) -> Iterator[Any]:
    class FixedDatetime(datetime):
        @classmethod
        def now(cls, tz: Any = None) -> datetime:
            fixed = datetime(2026, 1, 5, hour, tzinfo=UTC)
            return fixed.astimezone(tz) if tz is not None else fixed.replace(tzinfo=None)

    with patch.object(baseline.encode_scheduler, "datetime", FixedDatetime), patch.object(
        baseline.web_app, "runtime_encode_job_manifest_totals", return_value={}
    ) as telemetry, patch("subprocess.Popen", side_effect=AssertionError("Unexpected child process")), patch(
        "subprocess.run", side_effect=AssertionError("Unexpected child process")
    ):
        yield telemetry


def allocation_peak(operation: Callable[[], Any]) -> int:
    was_tracing = tracemalloc.is_tracing()
    if not was_tracing:
        tracemalloc.start()
    live_bytes, _ = tracemalloc.get_traced_memory()
    tracemalloc.reset_peak()
    try:
        result = operation()  # Keep the return value alive through the peak read.
        _, peak = tracemalloc.get_traced_memory()
        del result
        return max(0, peak - live_bytes)
    finally:
        if not was_tracing:
            tracemalloc.stop()


def verify_classification() -> list[dict[str, Any]]:
    """Check each case on its own so opposite errors cannot cancel in a total."""
    results = []
    with tempfile.TemporaryDirectory(prefix="mediaforce-pending-cases-") as directory:
        try:
            root = Path(directory)
            config, _ = baseline._fixture(root, 0)
            with open_db(config.paths.db_path) as connection:
                for kind, parent in [("shard", status) for status in baseline.PARENT_STATUSES] + [("single", None)]:
                    for case in CASES:
                        job = baseline._job(root, "case", case.status, job_kind=kind)
                        job.update(
                            parent_job_id=f"parent-{baseline.PARENT_STATUSES.index(parent)}" if parent else None,
                            host={"key": "fixture", "schedule_profile": case.profile, "schedule_timezone": "UTC"},
                            waiting_reason=case.reason, bypass_schedule=case.bypass,
                            progress={"progress_state": "schedule_waiting"} if case.interrupted else {},
                        )
                        encode_queue.save_encode_job(connection, job)
                        connection.commit()
                        for hour in (12, 23):
                            expected = int(case.counted if hour == 12 else case.name in OPEN_WAITING)
                            with fixture_runtime(hour=hour):
                                full = full_pending_read(connection, limit=1)
                                compact = encode_queue.list_pending_encode_jobs(connection, limit=1)
                                # Every input used by decoration must agree, even if irrelevant to this count.
                                assert compact == [{key: row[key] for key in compact[0]} for row in full]
                                queues = []
                                for read in (full_pending_read, encode_queue.list_pending_encode_jobs):
                                    with patch.object(encode_queue, "list_pending_encode_jobs", read):
                                        queue, _ = baseline._refresh(config)
                                        baseline._assert_counts(queue, 1, expected)
                                        queues.append(queue)
                                assert queues[0] == queues[1], (kind, parent, case.name, hour)
                            results.append({"kind": kind, "parent": parent, "case": case.name,
                                            "hour_utc": hour, "expected_waiting": expected, "passed": True})
            return results
        finally:
            reset_engine_cache()


def compare(pending_count: int, *, notes: str, iterations: int = 5,
            single_count: int = 0, extra_attention: int = 0, hour: int = 12) -> dict[str, Any]:
    if pending_count < len(CASES) or iterations < 1 or not 0 <= single_count <= pending_count or extra_attention < 0:
        raise ValueError("Use enough files for all cases, positive iterations and valid display controls")
    with tempfile.TemporaryDirectory(prefix="mediaforce-pending-comparison-") as directory:
        try:
            root = Path(directory)
            original_job = baseline._job

            def sized_job(job_root: Path, job_id: str, status: str, *, job_kind: str) -> dict[str, Any]:
                return {**original_job(job_root, job_id, status, job_kind=job_kind), "notes": NOTES[notes]}

            # Uneven frequencies prevent cancellation of aggregate classification errors.
            weighted_cases = (*CASES, CASES[0], CASES[0], CASES[3], CASES[-1])
            with patch.object(baseline, "CASES", weighted_cases), patch.object(baseline, "_job", sized_job):
                config, case_counts = baseline._fixture(root, pending_count)
            with open_db(config.paths.db_path) as connection:
                for index in range(single_count):
                    job = encode_queue.load_encode_job(connection, f"file-{index}")
                    assert job is not None
                    job.update(job_kind="single", parent_job_id=None)
                    encode_queue.save_encode_job(connection, job)
                for index in range(extra_attention):
                    encode_queue.save_encode_job(connection, sized_job(root, f"extra-{index}", "needs_attention", job_kind="folder"))
                connection.commit()
                counted_names = {case.name for case in CASES if case.counted} if hour == 12 else OPEN_WAITING
                expected = sum(case_counts[name] for name in counted_names)
                modes = {}
                payloads = []
                for mode, reader in (("full", full_pending_read), ("compact", encode_queue.list_pending_encode_jobs)):
                    with fixture_runtime(hour=hour) as telemetry, patch.object(encode_queue, "list_pending_encode_jobs", reader):
                        baseline._refresh(config)
                        samples = []
                        query_samples = []
                        for _ in range(iterations):
                            started = time.perf_counter()
                            rows = reader(connection, limit=pending_count)
                            query_samples.append((time.perf_counter() - started) * 1000)
                            del rows
                            queue, sample = baseline._refresh(config)
                            baseline._assert_counts(queue, pending_count, expected)
                            samples.append(sample)
                        hydration_peak = allocation_peak(lambda: reader(connection, limit=pending_count))
                        raw = encode_queue.summarize_encode_queue(connection, library_types=config.library_type_map)
                        decoration_peak = allocation_peak(lambda: baseline.web_app._decorate_encode_queue_for_scheduler(config, raw))
                        del raw
                        refresh_peak = allocation_peak(lambda: baseline._refresh(config))
                        queries = []

                        def record_query(_connection: Any, _cursor: Any, statement: str, _parameters: Any,
                                         _context: Any, _executemany: bool) -> None:
                            queries.append(statement)

                        event.listen(connection.engine, "before_cursor_execute", record_query)
                        telemetry.reset_mock()
                        try:
                            with patch.object(baseline.encode_scheduler, "encode_queue_schedule_profiles",
                                              wraps=baseline.encode_scheduler.encode_queue_schedule_profiles) as profiles:
                                queue, _ = baseline._refresh(config)
                                baseline._assert_counts(queue, pending_count, expected)
                        finally:
                            event.remove(connection.engine, "before_cursor_execute", record_query)
                        payloads.append(queue)
                        modes[mode] = {
                            "samples": samples, "pending_query_samples_ms": query_samples,
                            "median_pending_query_ms": statistics.median(query_samples),
                            "median_total_ms": statistics.median(float(sample["total_ms"]) for sample in samples),
                            "pending_hydration_peak_bytes": hydration_peak,
                            "decoration_incremental_peak_bytes": decoration_peak,
                            "refresh_peak_bytes": refresh_peak, "sql_statements": len(queries),
                            "sql_statements_text": queries, "profile_builds": profiles.call_count,
                            "display_telemetry_calls": telemetry.call_count,
                        }
                assert payloads[0] == payloads[1], "Full display payload or counts changed"
                return {"pending_files": pending_count, "notes": notes, "notes_bytes": len(NOTES[notes].encode()),
                        "single_files": single_count, "extra_attention_parents": extra_attention,
                        "hour_utc": hour, "case_counts": case_counts, "expected_waiting": expected,
                        "attention_rows": len(payloads[0]["needs_attention"]), "modes": modes, "passed": True}
        finally:
            reset_engine_cache()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-sha", required=True)
    parser.add_argument("--sizes", default="36,1000,10000")
    parser.add_argument("--iterations", type=int, default=5)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    payload = {
        "schema_version": 1, "source_sha": args.source_sha,
        "script_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "baseline_script_sha256": hashlib.sha256(Path(baseline.__file__).read_bytes()).hexdigest(),
        "source_file_sha256": {
            str(path): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in (Path("mediaforce/encoding/encode_queue.py"),
                         Path("mediaforce/web/runtime/encode_scheduler.py"))
        },
        "generated_at": datetime.now(UTC).isoformat(), "platform": platform.platform(),
        "python_version": platform.python_version(),
        "scope": "Synthetic warm shared refresh; isolated pending read uses an already-open connection; no route/transport latency",
        "classification": verify_classification(),
        "results": [compare(int(size), notes=notes, iterations=args.iterations)
                    for notes in NOTES for size in args.sizes.split(",")],
        "display_controls": [compare(36, notes="short", iterations=args.iterations, single_count=singles,
                                     extra_attention=attention, hour=hour)
                             for singles, attention, hour in ((18, 0, 12), (0, 30, 12), (18, 30, 23))],
    }
    args.output.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"output": str(args.output), "passed": True}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
