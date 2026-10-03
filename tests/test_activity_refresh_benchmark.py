from unittest.mock import patch

import pytest

from mediaforce.core.config import MediaforceConfig
from typing import Any

from scripts import benchmark_activity_refresh as benchmark


def test_synthetic_refresh_preserves_mixed_file_counts() -> None:
    result = benchmark.run_benchmark(72, iterations=1)
    assert result["observed_pending_files"] == result["pending_files"]
    assert result["observed_waiting_files"] == result["expected_waiting_files"]
    assert result["passed"]
    assert result["python_peak_bytes"] > 0
    assert result["sql_statements"] > 0
    assert result["profile_builds"] == 1
    assert result["job_decorations"] >= result["pending_files"]


def test_benchmark_rejects_a_wrong_schedule_count() -> None:
    original = benchmark.web_app._decorate_encode_queue_for_scheduler

    def wrong_count(config: MediaforceConfig, raw_queue: dict[str, Any]) -> dict[str, Any]:
        queue = original(config, raw_queue)
        queue["queued_schedule_waiting_count"] += 1
        return queue

    with patch.object(benchmark.web_app, "_decorate_encode_queue_for_scheduler", side_effect=wrong_count):
        with pytest.raises(AssertionError, match="Count mismatch"):
            benchmark.run_benchmark(36, iterations=1)


def test_benchmark_keeps_callers_allocation_tracing_enabled() -> None:
    was_tracing = benchmark.tracemalloc.is_tracing()
    benchmark.tracemalloc.start()
    try:
        benchmark.run_benchmark(36, iterations=1)
        assert benchmark.tracemalloc.is_tracing()
    finally:
        if was_tracing:
            benchmark.tracemalloc.start()
        else:
            benchmark.tracemalloc.stop()
