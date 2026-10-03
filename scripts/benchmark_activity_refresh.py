"""Measure the queue-summary component shared by Activity and folder requests."""

import argparse
import hashlib
import json
import platform
import statistics
import tempfile
import time
import tracemalloc
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from unittest.mock import patch

from sqlalchemy import event
from starlette.responses import JSONResponse

from mediaforce.core.config import ConfigPaths, MediaforceConfig
from mediaforce.core.db import open_db, reset_engine_cache
from mediaforce.encoding.encode_queue import save_encode_job, summarize_encode_queue
from mediaforce.web import app as web_app
from mediaforce.web.runtime import encode_scheduler


@dataclass(frozen=True, slots=True)
class WaitCase:
    name: str
    counted: bool
    status: str = "queued"
    profile: str = "always"
    reason: str | None = None
    interrupted: bool = False
    bypass: bool = False


CASES = (
    WaitCase("closed_profile", True, profile="fixture_night"),
    WaitCase("window_reason", True, reason="Waiting for a host schedule window."),
    WaitCase("interrupted", True, interrupted=True),
    WaitCase("interrupted_storage", False, reason="Waiting for shared storage.", interrupted=True),
    WaitCase("interrupted_host", False, reason="waiting for an available encode host", interrupted=True),
    WaitCase("short_window", True, reason="waiting for a host window with enough time remaining"),
    WaitCase("retry", False, status="retry_backoff", reason="waiting before retrying"),
    WaitCase("storage", False, reason="Waiting for shared storage."),
    WaitCase("host", False, reason="Waiting for an encode computer."),
    WaitCase("impossible_fit", False, reason="Estimated runtime longer than every configured host schedule window"),
    WaitCase("bypass", False, profile="never", reason="Waiting for a host schedule window.", bypass=True),
    WaitCase("ready", False),
)
PARENT_STATUSES = ("needs_attention", "queued", "running")


class FixtureDatetime(datetime):
    @classmethod
    def now(cls, tz: Any = None) -> datetime:
        fixed = datetime(2026, 1, 5, 12, tzinfo=UTC)
        return fixed.astimezone(tz) if tz is not None else fixed.replace(tzinfo=None)


def _fixture(root: Path, pending_count: int) -> tuple[MediaforceConfig, dict[str, int]]:
    config = MediaforceConfig(
        raw={
            "media": {"source_roots": {"tv": str(root / "source")}},
            "remote_hosts": [],
            "encode_queue": {
                "scheduler": {"mode": "anytime", "timezone": "UTC"},
                "schedule_profiles": [{"key": "fixture_night", "mode": "night", "timezone": "UTC",
                                       "start_hour": 22, "end_hour": 8}],
            },
        },
        paths=ConfigPaths(
            project_root=root, config_path=root / "config.toml", db_path=root / "fixture.sqlite3",
            run_manifest_dir=root / "runs", web_state_dir=root / "web", review_dir=root / "review",
            runtime_settings_path=root / "runtime.json",
        ),
    )
    case_counts = dict.fromkeys((case.name for case in CASES), 0)
    with open_db(config.paths.db_path) as connection:
        for parent_index, status in enumerate(PARENT_STATUSES):
            save_encode_job(connection, _job(root, f"parent-{parent_index}", status, job_kind="folder"))
        for index in range(pending_count):
            case = CASES[index % len(CASES)]
            case_counts[case.name] += 1
            job = _job(root, f"file-{index}", case.status, job_kind="shard")
            job.update(
                parent_job_id=f"parent-{(index // len(CASES)) % len(PARENT_STATUSES)}",
                host={"key": "fixture", "schedule_profile": case.profile, "schedule_timezone": "UTC"},
                waiting_reason=case.reason, bypass_schedule=case.bypass,
                progress={"progress_state": "schedule_waiting"} if case.interrupted else {},
            )
            save_encode_job(connection, job)
        connection.commit()
    return config, case_counts


def _job(root: Path, job_id: str, status: str, *, job_kind: str) -> dict[str, Any]:
    return {
        "job_id": job_id, "prefix": "tv/Fixture/Season 01", "job_kind": job_kind,
        "status": status, "manifest_path": str(root / "runs" / f"{job_id}.json"),
        "item_count": 1, "notes": "Synthetic queue entry. " * 48,
        "created_at": "2026-01-01T00:00:00+00:00", "updated_at": "2026-01-01T00:00:00+00:00",
    }


def _refresh(config: MediaforceConfig) -> tuple[dict[str, Any], dict[str, float | int]]:
    started = time.perf_counter()
    with open_db(config.paths.db_path) as connection:
        queue = summarize_encode_queue(connection, library_types=config.library_type_map)
    read_finished = time.perf_counter()
    queue = web_app._decorate_encode_queue_for_scheduler(config, queue)
    decoration_finished = time.perf_counter()
    response = JSONResponse({"encode_queue": queue})
    finished = time.perf_counter()
    return queue, {
        "summary_ms": (read_finished - started) * 1000,
        "decoration_ms": (decoration_finished - read_finished) * 1000,
        "serialization_ms": (finished - decoration_finished) * 1000,
        "total_ms": (finished - started) * 1000,
        "response_bytes": len(response.body),
    }


def _assert_counts(queue: dict[str, Any], pending_count: int, expected_waiting: int) -> None:
    if queue["pending_work_count"] != pending_count or queue["queued_schedule_waiting_count"] != expected_waiting:
        raise AssertionError(f"Count mismatch: pending={queue['pending_work_count']}, waiting={queue['queued_schedule_waiting_count']}")
    if "pending" in queue:
        raise AssertionError("Internal inventory leaked into response")


def run_benchmark(pending_count: int, *, iterations: int = 5) -> dict[str, Any]:
    if pending_count < len(CASES) * len(PARENT_STATUSES) or iterations < 1:
        raise ValueError("Use at least 36 pending files and one iteration to cover every case and parent state")
    with tempfile.TemporaryDirectory(prefix="mediaforce-activity-benchmark-") as directory:
        try:
            config, case_counts = _fixture(Path(directory), pending_count)
            expected_waiting = sum(case_counts[case.name] for case in CASES if case.counted)
            # Fixture display rows use stubbed totals; no manifests exist in this inventory-only fixture.
            with patch.object(encode_scheduler, "datetime", FixtureDatetime), patch.object(
                web_app, "runtime_encode_job_manifest_totals", return_value={}
            ) as manifest_reads, patch(
                "subprocess.Popen", side_effect=AssertionError("Unexpected child process")
            ), patch("subprocess.run", side_effect=AssertionError("Unexpected child process")):
                _refresh(config)  # Warm the engine and scheduler; exclude setup from all measurements.
                samples = []
                for _ in range(iterations):
                    queue, sample = _refresh(config)
                    _assert_counts(queue, pending_count, expected_waiting)
                    samples.append(sample)
                was_tracing = tracemalloc.is_tracing()
                if not was_tracing:
                    tracemalloc.start()
                baseline_bytes, _ = tracemalloc.get_traced_memory()
                tracemalloc.reset_peak()
                try:
                    queue, _ = _refresh(config)
                    _assert_counts(queue, pending_count, expected_waiting)
                    _, total_peak_bytes = tracemalloc.get_traced_memory()
                    peak_bytes = max(0, total_peak_bytes - baseline_bytes)
                finally:
                    if not was_tracing:
                        tracemalloc.stop()
                queries: list[str] = []

                def record_query(_connection: Any, _cursor: Any, statement: str, _parameters: Any,
                                 _context: Any, _executemany: bool) -> None:
                    queries.append(statement)

                with open_db(config.paths.db_path) as connection:
                    engine = connection.engine
                event.listen(engine, "before_cursor_execute", record_query)
                manifest_reads.reset_mock()
                try:
                    with patch.object(encode_scheduler, "encode_queue_schedule_profiles", wraps=encode_scheduler.encode_queue_schedule_profiles) as profiles, patch.object(
                        encode_scheduler, "decorate_encode_job_for_scheduler", wraps=encode_scheduler.decorate_encode_job_for_scheduler,
                    ) as decorations:
                        queue, _ = _refresh(config)
                        _assert_counts(queue, pending_count, expected_waiting)
                finally:
                    event.remove(engine, "before_cursor_execute", record_query)
                return {
                    "pending_files": pending_count, "iterations": iterations,
                    "fixture_case_counts": case_counts, "parent_states": list(PARENT_STATUSES),
                    "expected_waiting_files": expected_waiting,
                    "observed_waiting_files": queue["queued_schedule_waiting_count"],
                    "observed_pending_files": queue["pending_work_count"],
                    "samples": samples,
                    "median_total_ms": statistics.median(float(sample["total_ms"]) for sample in samples),
                    "python_peak_bytes": peak_bytes,
                    "sql_statements": len(queries), "sql_statements_text": queries,
                    "profile_builds": profiles.call_count, "job_decorations": decorations.call_count,
                    "display_manifest_total_calls": manifest_reads.call_count,
                    "passed": True,
                }
        finally:
            reset_engine_cache()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sizes", default="36,1000,10000")
    parser.add_argument("--iterations", type=int, default=5)
    parser.add_argument("--source-sha", required=True, help="Revision being measured; supplied by the caller")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    payload = {
        "schema_version": 1, "source_sha": args.source_sha,
        "benchmark_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "generated_at": datetime.now(UTC).isoformat(),
        "python_version": platform.python_version(), "platform": platform.platform(),
        "scope": "Shared queue summary, schedule decoration and JSON response encoding; excludes other route work and transport",
        "results": [run_benchmark(int(size), iterations=args.iterations) for size in args.sizes.split(",")],
    }
    encoded = json.dumps(payload, indent=2) + "\n"
    if args.output:
        args.output.write_text(encoded, encoding="utf-8")
    print(encoded, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
