from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from mediaforce.web import app as web_app
from mediaforce.web.runtime.encode_runtime import _selected_encode_schedule_close_deadline
from mediaforce.encoding.duration_estimate import estimate_encode_job_duration, estimate_fits_before_schedule_close


@pytest.mark.parametrize("finish_current_file", [False, True])
def test_finish_file_window_preserves_work_without_a_close_deadline(finish_current_file: bool) -> None:
    policy = web_app._normalize_encode_queue_scheduler({
        "mode": "night", "timezone": "host_local", "start_hour": 0, "end_hour": 5,
        "finish_current_file": finish_current_file,
    })
    host = {"schedule_closes_at": "2026-10-28T09:00:00+00:00", "finish_current_file": policy.get("finish_current_file")}
    deadline = _selected_encode_schedule_close_deadline({"item_count": 1}, host)
    assert (deadline is None) is finish_current_file
    now = datetime(2026, 10, 28, 8, 59, 59, tzinfo=ZoneInfo("UTC"))
    estimate = estimate_encode_job_duration([{"probe": {"duration_seconds": 3600}}], host, ())
    assert not estimate_fits_before_schedule_close(estimate, host, now=now)
    maximum = web_app._max_encode_schedule_window_seconds(policy, now=now)
    assert maximum is not None
    assert maximum == web_app._max_encode_schedule_window_seconds({**policy, "finish_current_file": False}, now=now)


def test_legacy_multi_file_jobs_keep_their_hard_stop() -> None:
    host = {"schedule_closes_at": "2026-10-28T09:00:00+00:00", "finish_current_file": True}
    assert _selected_encode_schedule_close_deadline({"item_count": 2}, host) == host["schedule_closes_at"]
