from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from mediaforce.web import app as web_app
from mediaforce.web.runtime.encode_runtime import _selected_encode_schedule_close_deadline
from mediaforce.encoding.duration_estimate import estimate_encode_job_duration, estimate_fits_before_schedule_close


@pytest.mark.parametrize("day", ["2026-10-28", "2026-11-01", "2027-03-14"])
@pytest.mark.parametrize(
    ("hour", "minute", "second", "allowed"),
    [(23, 59, 59, False), (0, 0, 0, True), (4, 59, 59, True), (5, 0, 0, False), (12, 0, 0, False)],
)
def test_night_window_admits_only_midnight_through_before_five(
    day: str, hour: int, minute: int, second: int, allowed: bool,
) -> None:
    policy = web_app._normalize_encode_queue_scheduler({
        "mode": "night", "timezone": "host_local", "start_hour": 0, "end_hour": 5,
        "finish_current_file": True,
    })
    now = datetime.fromisoformat(day).replace(hour=hour, minute=minute, second=second, tzinfo=ZoneInfo("America/New_York"))
    assert web_app._scheduler_allows_encode_run(policy, now=now, host_payload={"timezone": "America/New_York"}) is allowed


@pytest.mark.parametrize("finish_current_file", [False, True])
def test_finish_file_window_preserves_work_without_a_close_deadline(finish_current_file: bool) -> None:
    policy = web_app._normalize_encode_queue_scheduler({
        "mode": "night", "timezone": "host_local", "start_hour": 0, "end_hour": 5,
        "finish_current_file": finish_current_file,
    })
    host = {"schedule_closes_at": "2026-10-28T09:00:00+00:00", "finish_current_file": policy.get("finish_current_file")}
    deadline = _selected_encode_schedule_close_deadline({}, host)
    assert (deadline is None) is finish_current_file
    now = datetime(2026, 10, 28, 8, 59, 59, tzinfo=ZoneInfo("UTC"))
    estimate = estimate_encode_job_duration([{"probe": {"duration_seconds": 3600}}], host, ())
    assert estimate_fits_before_schedule_close(estimate, host, now=now) is finish_current_file
    maximum = web_app._max_encode_schedule_window_seconds(policy, now=now)
    assert (maximum is None) is finish_current_file
