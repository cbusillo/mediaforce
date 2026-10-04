from typing import Any
from unittest.mock import patch

import pytest

from mediaforce.core.db import DBClient
from mediaforce.encoding import encode_queue
from scripts import benchmark_pending_inventory as benchmark


def test_compact_read_agrees_for_each_case_clock_parent_and_single() -> None:
    results = benchmark.verify_classification()
    assert len(results) == len(benchmark.CASES) * (len(benchmark.baseline.PARENT_STATUSES) + 1) * 2
    assert all(result["passed"] for result in results)


@pytest.mark.parametrize("singles,attention,hour", [(0, 0, 12), (18, 0, 12), (0, 30, 12), (18, 30, 23)])
def test_comparison_preserves_counts_and_complete_display_payload(singles: int, attention: int, hour: int) -> None:
    result = benchmark.compare(36, notes="short", iterations=1, single_count=singles,
                               extra_attention=attention, hour=hour)
    assert result["passed"]
    full, compact = result["modes"]["full"], result["modes"]["compact"]
    assert full["sql_statements"] == compact["sql_statements"]
    assert full["display_telemetry_calls"] == compact["display_telemetry_calls"]
    assert full["profile_builds"] == compact["profile_builds"] == 1
    assert result["attention_rows"] >= attention


def test_classification_rejects_dropped_schedule_input() -> None:
    original = encode_queue.list_pending_encode_jobs

    def corrupted_read(connection: DBClient, *, limit: int) -> list[dict[str, Any]]:
        return [{**row, "bypass_schedule": False} for row in original(connection, limit=limit)]

    with patch.object(encode_queue, "list_pending_encode_jobs", corrupted_read), pytest.raises(AssertionError):
        benchmark.verify_classification()


def test_comparison_rejects_display_telemetry_corruption() -> None:
    original = benchmark.baseline.web_app._decorate_encode_queue_for_scheduler
    def corrupted_decoration(config: Any, queue: dict[str, Any]) -> dict[str, Any]:
        result = original(config, queue)
        # Mutation occurs only in the compact pass; counts still agree.
        if encode_queue.list_pending_encode_jobs is not benchmark.full_pending_read:
            result["queued"][0]["attempt_summary"] = "lost display telemetry"
        return result

    with patch.object(benchmark.baseline.web_app, "_decorate_encode_queue_for_scheduler", corrupted_decoration):
        with pytest.raises(AssertionError, match="Full display payload"):
            benchmark.compare(36, notes="empty", iterations=1)


def test_allocation_measurement_preserves_caller_tracing() -> None:
    was_tracing = benchmark.tracemalloc.is_tracing()
    benchmark.tracemalloc.start()
    try:
        assert benchmark.allocation_peak(lambda: bytearray(4096)) >= 4096
        assert benchmark.tracemalloc.is_tracing()
    finally:
        if not was_tracing:
            benchmark.tracemalloc.stop()
