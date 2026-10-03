from typing import Any
from unittest.mock import patch

import pytest

from mediaforce.core.config import MediaforceConfig

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


@pytest.mark.parametrize("defect", ("waiting_count", "pending_count", "inventory_leak"))
def test_benchmark_rejects_corrupted_queue_payloads(defect: str) -> None:
    original = benchmark.web_app._decorate_encode_queue_for_scheduler

    def corrupt_payload(config: MediaforceConfig, raw_queue: dict[str, Any]) -> dict[str, Any]:
        queue = original(config, raw_queue)
        if defect == "inventory_leak":
            queue["pending"] = [{"job_id": "unexpected-file"}]
        else:
            count_key = "pending_work_count" if defect == "pending_count" else "queued_schedule_waiting_count"
            queue[count_key] += 1
        return queue

    with patch.object(benchmark.web_app, "_decorate_encode_queue_for_scheduler", side_effect=corrupt_payload):
        with pytest.raises(AssertionError):
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
