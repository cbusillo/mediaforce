import argparse
import json
import resource
import subprocess
import sys
import tempfile
import time
import tracemalloc
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast
from unittest.mock import patch

from sqlalchemy import func, select
from sqlalchemy.exc import OperationalError

from mediaforce.core.config import DEFAULT_CONFIG_PATH, ConfigPaths, MediaforceConfig, load_config
from mediaforce.core.db import DBClient, open_db, reset_engine_cache
from mediaforce.core.db_tables import library_items
from mediaforce.core.utils import content_version_fingerprint
from mediaforce.library.evidence_state import rebuild_library_item_evidence_states
from mediaforce.library.scanner import scan_library


@dataclass(slots=True)
class BenchmarkResult:
    item_count: int
    iterations: int
    wall_seconds: float
    cpu_seconds: float
    python_peak_bytes: int
    process_peak_rss_bytes: int
    child_process_count: int
    discovered: int
    reprobed: int
    unchanged: int
    db_operation_count: int
    db_operation_seconds: float
    db_lock_wait_events: int
    db_lock_wait_seconds: float
    db_lock_errors: int
    passed: bool


class _MeasuredConnection:
    def __init__(self, connection: DBClient, lock_wait_threshold_seconds: float) -> None:
        self.connection = connection
        self.lock_wait_threshold_seconds = lock_wait_threshold_seconds
        self.operation_count = 0
        self.operation_seconds = 0.0
        self.lock_wait_events = 0
        self.lock_wait_seconds = 0.0
        self.lock_errors = 0

    def execute(self, *args: Any, **kwargs: Any) -> Any:
        return self._measure(self.connection.execute, *args, **kwargs)

    def exec_driver_sql(self, *args: Any, **kwargs: Any) -> Any:
        return self._measure(self.connection.exec_driver_sql, *args, **kwargs)

    def commit(self) -> None:
        self._measure(self.connection.commit)

    def _measure(self, operation: Any, *args: Any, **kwargs: Any) -> Any:
        started_at = time.perf_counter()
        try:
            return operation(*args, **kwargs)
        except OperationalError as exc:
            if "locked" in str(exc).lower() or "busy" in str(exc).lower():
                self.lock_errors += 1
            raise
        finally:
            elapsed = time.perf_counter() - started_at
            self.operation_count += 1
            self.operation_seconds += elapsed
            if elapsed >= self.lock_wait_threshold_seconds:
                self.lock_wait_events += 1
                self.lock_wait_seconds += elapsed


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Benchmark unchanged catalog refreshes without launching media-analysis subprocesses."
    )
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG_PATH)
    parser.add_argument("--sizes", default="1000,10000,current,2x-current")
    parser.add_argument("--iterations", type=int, default=2)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--lock-wait-threshold-ms", type=float, default=50.0)
    args = parser.parse_args()

    current_count = _current_catalog_count(args.config)
    sizes, skipped = _resolve_sizes(args.sizes, current_count)
    results = [
        run_benchmark(
            item_count,
            iterations=args.iterations,
            lock_wait_threshold_seconds=max(args.lock_wait_threshold_ms, 0.0) / 1000.0,
        )
        for item_count in sizes
    ]
    payload = {
        "schema_version": 1,
        "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "current_catalog_count": current_count,
        "requested_sizes": args.sizes,
        "skipped": skipped,
        "results": [asdict(result) for result in results],
    }
    encoded = json.dumps(payload, indent=2, sort_keys=True) + "\n"
    if args.output is not None:
        args.output.expanduser().write_text(encoded, encoding="utf-8")
    print(encoded, end="")
    return 0 if all(result.passed for result in results) else 1


def run_benchmark(
        item_count: int,
        *,
        iterations: int = 2,
        lock_wait_threshold_seconds: float = 0.05,
) -> BenchmarkResult:
    if item_count <= 0:
        raise ValueError("item_count must be positive")
    if iterations <= 0:
        raise ValueError("iterations must be positive")

    with tempfile.TemporaryDirectory(prefix="mediaforce-catalog-benchmark-") as temp_dir:
        project_root = Path(temp_dir)
        media_root = project_root / "library"
        media_root.mkdir()
        config = _benchmark_config(project_root, media_root)
        _seed_fixture_catalog(config, media_root, item_count)

        child_process_count = 0

        def blocked_child_process(*_args: Any, **_kwargs: Any) -> None:
            nonlocal child_process_count
            child_process_count += 1
            raise subprocess.SubprocessError("benchmark blocked unexpected child process")

        usage_before = resource.getrusage(resource.RUSAGE_SELF)
        tracemalloc.start()
        started_at = time.perf_counter()
        discovered = 0
        reprobed = 0
        unchanged = 0
        try:
            with open_db(config.paths.db_path) as connection:
                measured_connection = _MeasuredConnection(connection, lock_wait_threshold_seconds)
                with patch("subprocess.run", side_effect=blocked_child_process), patch(
                    "subprocess.Popen",
                    side_effect=blocked_child_process,
                ):
                    for _ in range(iterations):
                        stats = scan_library(cast(DBClient, measured_connection), config)
                        discovered += stats.discovered
                        reprobed += stats.reprobed
                        unchanged += stats.unchanged
        finally:
            wall_seconds = time.perf_counter() - started_at
            _, python_peak_bytes = tracemalloc.get_traced_memory()
            tracemalloc.stop()
            usage_after = resource.getrusage(resource.RUSAGE_SELF)
            reset_engine_cache()

    cpu_seconds = (
        usage_after.ru_utime - usage_before.ru_utime
        + usage_after.ru_stime - usage_before.ru_stime
    )
    passed = (
        child_process_count == 0
        and discovered == 0
        and reprobed == 0
        and unchanged == item_count * iterations
    )
    return BenchmarkResult(
        item_count=item_count,
        iterations=iterations,
        wall_seconds=round(wall_seconds, 6),
        cpu_seconds=round(cpu_seconds, 6),
        python_peak_bytes=python_peak_bytes,
        process_peak_rss_bytes=_peak_rss_bytes(usage_after.ru_maxrss),
        child_process_count=child_process_count,
        discovered=discovered,
        reprobed=reprobed,
        unchanged=unchanged,
        db_operation_count=measured_connection.operation_count,
        db_operation_seconds=round(measured_connection.operation_seconds, 6),
        db_lock_wait_events=measured_connection.lock_wait_events,
        db_lock_wait_seconds=round(measured_connection.lock_wait_seconds, 6),
        db_lock_errors=measured_connection.lock_errors,
        passed=passed,
    )


def _benchmark_config(project_root: Path, media_root: Path) -> MediaforceConfig:
    paths = ConfigPaths(
        project_root=project_root,
        config_path=project_root / "config.toml",
        db_path=project_root / "library.sqlite3",
        run_manifest_dir=project_root / "runs",
        web_state_dir=project_root / "web",
        review_dir=project_root / "review",
        runtime_settings_path=project_root / "runtime-settings.json",
    )
    return MediaforceConfig(
        raw={
            "media": {
                "libraries": [
                    {
                        "key": "benchmark",
                        "label": "Benchmark",
                        "path": str(media_root),
                        "type": "other",
                        "availability": "browse_only",
                    }
                ]
            },
            "video": {},
            "audio": {},
            "subtitle": {},
            "planning": {},
            "validation": {},
            "overrides": [],
            "remote_hosts": [],
        },
        paths=paths,
    )


def _seed_fixture_catalog(config: MediaforceConfig, media_root: Path, item_count: int) -> None:
    payload = b"mediaforce-catalog-refresh-fixture\n"
    now = datetime.now(UTC).isoformat(timespec="seconds")
    rows: list[dict[str, Any]] = []
    shared_content_fingerprint: str | None = None
    evidence_values = (None, "not-json", '{"retry_required":true}')
    with open_db(config.paths.db_path) as connection:
        for index in range(item_count):
            file_name = f"item-{index:08d}.mkv"
            file_path = media_root / file_name
            file_path.write_bytes(payload)
            stat_result = file_path.stat()
            if shared_content_fingerprint is None:
                shared_content_fingerprint = content_version_fingerprint(file_path, stat_result)
            rows.append(
                {
                    "source_path": str(file_path),
                    "rel_path": f"benchmark/{file_name}",
                    "media_root": "benchmark",
                    "parent_dir": "benchmark",
                    "file_name": file_name,
                    "container": ".mkv",
                    "size_bytes": stat_result.st_size,
                    "mtime_ns": stat_result.st_mtime_ns,
                    "fingerprint": f"fixture-{index}",
                    "duration_seconds": 1.0,
                    "video_codec": "h264",
                    "audio_track_count": 1,
                    "subtitle_track_count": 0,
                    "english_audio_count": 1,
                    "english_subtitle_count": 0,
                    "audio_summary_json": "[]",
                    "subtitle_summary_json": "[]",
                    "attachment_summary_json": "[]",
                    "cadence_summary_json": evidence_values[index % len(evidence_values)],
                    "media_fingerprint_json": evidence_values[(index + 1) % len(evidence_values)],
                    "content_version_changed_at": now,
                    "content_version_fingerprint": shared_content_fingerprint,
                    "status": "discovered",
                    "priority_score": 0,
                    "last_scan_id": "benchmark-seed",
                    "discovered_at": now,
                    "last_seen_at": now,
                    "updated_at": now,
                }
            )
            if len(rows) >= 1_000:
                connection.execute(library_items.insert(), rows)
                connection.commit()
                rows.clear()
        if rows:
            connection.execute(library_items.insert(), rows)
            connection.commit()
        rebuild_library_item_evidence_states(connection)
        connection.commit()
    reset_engine_cache()


def _current_catalog_count(config_path: Path) -> int:
    try:
        config = load_config(config_path.expanduser())
        with open_db(config.paths.db_path) as connection:
            return int(
                connection.execute(
                    select(func.count())
                    .select_from(library_items)
                    .where(library_items.c.status != "missing")
                ).scalar_one()
            )
    except (OSError, RuntimeError, TypeError, ValueError):
        return 0


def _resolve_sizes(raw_sizes: str, current_count: int) -> tuple[list[int], list[str]]:
    sizes: list[int] = []
    skipped: list[str] = []
    for raw_token in raw_sizes.split(","):
        token = raw_token.strip().lower()
        if not token:
            continue
        if token == "current":
            value = current_count
        elif token == "2x-current":
            value = current_count * 2
        else:
            try:
                value = int(token.replace("_", ""))
            except ValueError as exc:
                raise ValueError(f"Unsupported size token: {raw_token}") from exc
        if value <= 0:
            skipped.append(raw_token.strip())
            continue
        if value not in sizes:
            sizes.append(value)
    if not sizes:
        raise ValueError("No positive benchmark sizes were resolved")
    return sizes, skipped


def _peak_rss_bytes(raw_value: int | float) -> int:
    value = int(raw_value)
    return value if sys.platform == "darwin" else value * 1024


if __name__ == "__main__":
    raise SystemExit(main())
